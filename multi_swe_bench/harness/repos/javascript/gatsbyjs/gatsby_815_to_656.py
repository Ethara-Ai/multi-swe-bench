import re
import shlex
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.test_result import get_modified_files

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")

# `diff --git a/<old> b/<new>` — group(2) is the post-image path, which is
# present for created files too (where the `--- a/` side is `/dev/null`).
_DIFF_GIT_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)", re.MULTILINE)


def _gold_test_paths(test_patch: str) -> tuple[list[str], list[str]]:
    """Split the gold test patch's targets into (pre-existing, newly-created).

    ``get_modified_files`` deliberately drops entries whose ``---`` side is
    ``/dev/null``, so it only ever reports test files that already exist at
    BASE_COMMIT. For this repo that is a minority of the gold tests: most of
    Gatsby's PRs *add* their spec/fixture files. Both halves have to be
    restored before grading, but by different means — a pre-existing file is
    checked back out of BASE_COMMIT, a created file is deleted so the gold
    test.patch can lay down its own copy.
    """
    text = (test_patch or "").replace("\r\n", "\n").replace("\r", "\n")
    all_paths = {m.group(2) for m in _DIFF_GIT_RE.finditer(text)}
    existing = {p for p in get_modified_files(test_patch or "")}
    created = all_paths - existing
    return sorted(existing), sorted(created)


def _restore_gold_tests(test_patch: str, base_sha: str) -> str:
    """Shell that reverts any agent edit to a gold test file.

    Defence in depth for the reward-hacking contract (MSB-REWARD-003). The
    grader already refuses a fix patch that touches a gold test file, but that
    pre-run check reads ``get_modified_files`` and is therefore blind to gold
    tests the test patch *creates*. Re-establishing the gold tests inside the
    image closes that gap regardless of what the submitted patch did.
    """
    existing, created = _gold_test_paths(test_patch)
    lines = []
    if existing:
        lines.append(
            f"git checkout {base_sha} -- {' '.join(shlex.quote(p) for p in existing)} || true"
        )
    if created:
        lines.append(f"rm -f {' '.join(shlex.quote(p) for p in created)}")
    if not lines:
        return "true"
    return "\n".join(lines)


class ImageBase(Image):
    """The single shared base image for every gatsbyjs/gatsby PR.

    Tagged ``:base`` and identical in both era modules on purpose. ``Image``
    hashes and compares on ``image_full_name()``, so every PR's base collapses
    to one entry in ``build_dataset``'s dependency graph and the image is built
    **once** and reused by all PRs in both eras.

    Deliberately NOT pinned and NOT hardened. It clones the repo unpinned
    (whatever HEAD is, no BASE_COMMIT, no checkout) and warms the apt/npm
    layers once; each PR image checks out its own BASE_COMMIT on top and runs
    ``Image._HARDENING_BLOCK`` there. Pinning or stripping the shared base
    would prune every *other* PR's commit out of the one clone they all share.
    Mirrors ``yarnpkg/yarn.py``'s ``YarnImageBase`` and p5.js's ``ImageBase``.

    The clone is ``--depth 1``: under QEMU ``linux/arm64`` emulation git 2.11
    (node:8) dies with ``fatal: Out of memory, getdelim failed`` on Gatsby's
    full history. Verified repo-size dependent — the same command succeeds on a
    shallow tree. Each PR image fetches its own commit shallowly on top.

    Carries the ``# syntax=`` directive so ``DockerfileEnhancer`` emits it
    verbatim instead of splicing its BASE_COMMIT checkout + hardening tail in
    (this image has no BASE_COMMIT to check out). No proxy/CA plumbing: the
    build reaches github.com and the npm registry over the base image's own
    trust store.
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

    def dependency(self) -> str:
        return "node:8"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo
        return f"""\
# syntax=docker/dockerfile:1.6
FROM node:8

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC
ENV LANG=C.UTF-8

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="ethara.ai"

WORKDIR /home/

RUN npm install -g npm@6 || true

# --depth 1: the per-PR `git checkout` below OOMs under QEMU linux/arm64
# on Gatsby's full history (`fatal: Out of memory, getdelim failed`).
# Verified repo-size dependent. Drop `--depth 1` here and in the per-PR
# fetch to match the template exactly -- that builds amd64 only.
RUN git clone --depth 1 "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

RUN npm install || true

CMD ["/bin/bash"]
"""


class ImageDefault(Image):
    """Per-PR image for the Gatsby v0 / pre-monorepo era (PRs 656-815):
    single package, top-level test/ directory, AVA runner, Node 8.

    Thin layer on top of the shared ``:base``: fetch this PR's BASE_COMMIT,
    reconcile dependencies for that commit, then strip the git history. Because
    ``dependency()`` returns an ``Image`` (not a string),
    ``DockerfileEnhancer.enhance`` returns this Dockerfile verbatim, so
    ``BASE_COMMIT`` is declared here rather than injected.
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
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        restore = _restore_gold_tests(self.pr.test_patch, self.pr.base.sha)

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
# Reconcile the shared base's warmed node_modules with THIS commit's
# package.json. Runs while the network is still reachable and before the
# hardening strip, which removes the origin remote.
set -uxo pipefail
cd /home/{pr.repo}
npm install --no-audit --no-fund 2>/dev/null || npm install --no-audit --no-fund --ignore-scripts 2>/dev/null || true
./node_modules/.bin/ava --version 2>/dev/null || npm install --no-save ava@0.19 2>/dev/null || true
exit 0
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -uo pipefail
export CI=true
cd /home/{pr.repo}
timeout -k 60 3600 ./node_modules/.bin/ava --tap test 2>&1 || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -uo pipefail
export CI=true
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
timeout -k 60 3600 ./node_modules/.bin/ava --tap test 2>&1 || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -uo pipefail
export CI=true
cd /home/{pr.repo}
# Fix patch first, on the clean BASE_COMMIT tree: at evaluation time this file
# is overwritten with the *agent's* patch, so it must never be applied on top
# of the gold tests.
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
# Reward-hacking guard: discard anything the fix patch did to a gold test file
# before the gold tests are laid down.
{restore}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
timeout -k 60 3600 ./node_modules/.bin/ava --tap test 2>&1 || true
""".format(pr=self.pr, restore=restore),
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        repo = self.pr.repo
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        header = f"""\
FROM {base.image_name()}:{base.image_tag()}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

WORKDIR /home/{repo}

{copy_commands}
RUN git fetch --depth 1 origin "${{BASE_COMMIT}}" && \
    git checkout --detach "${{BASE_COMMIT}}"

RUN bash /home/prepare.sh

# The base image's `git clone` leaves the symbolic ref refs/remotes/origin/HEAD.
# The hardening block below drops the origin remote first, which strips
# refs/remotes/origin/* and leaves that symref dangling; `git for-each-ref`
# then only warns about the broken ref instead of listing it, so the
# `update-ref -d` sweep never removes it and `git gc` aborts with
# `fatal: bad object refs/remotes/origin/HEAD`. Delete it while origin
# still exists. Kept after prepare.sh so the cached npm-install layer
# stays valid.
RUN git remote set-head origin --delete 2>/dev/null || true; \
    git update-ref -d refs/remotes/origin/HEAD 2>/dev/null || true; \
    rm -f .git/refs/remotes/origin/HEAD

"""

        tail = f"""
{self.clear_env}

CMD ["/bin/bash"]
"""

        return header + Image._HARDENING_BLOCK + tail


def parse_ava_tap(log: str) -> TestResult:
    """Parse AVA TAP output: `ok N - title` / `not ok N - title` / `# SKIP`."""
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    re_ok = re.compile(r"^ok \d+ - (.+)$")
    re_not_ok = re.compile(r"^not ok \d+ - (.+)$")

    # AVA machinery messages emitted as TAP points but not real tests.
    _noise = ("No tests found", "Couldn't find any", "Error: ")

    for line in log.splitlines():
        line = ANSI_ESCAPE.sub("", line).strip()
        m = re_not_ok.match(line)
        if m:
            name = m.group(1).strip()
            if any(name.startswith(p) or p in name for p in _noise):
                continue
            failed_tests.add(name)
            continue
        m = re_ok.match(line)
        if m:
            name = m.group(1).strip()
            if "# SKIP" in name or "# skip" in name:
                skipped_tests.add(re.split(r"# [Ss][Kk][Ii][Pp]", name)[0].strip())
            else:
                passed_tests.add(name)

    passed_tests -= failed_tests
    passed_tests -= skipped_tests
    skipped_tests -= failed_tests

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


# `number_interval` enumerates the bundle, it is NOT a range. A range such as
# "656-838" would assert that every PR between the endpoints is part of the
# instance; these bundles are sparse, so the interval lists exactly the PRs
# squashed into it, joined by `-` — the form `build_lht_dataset.py` emits.
# `Instance.create` resolves by the exact key `f"{pr.org}/{pr.number_interval}"`,
# so every bundle needs its own registration. Regenerate with:
#     "-".join(str(n) for n in row["prs_in_bundle"])
_BUNDLES = (
    "656-695",
    "666-668-671",
    "681-683-690",
    "764-765-768-773-775-779-782-783-784",
    "804-806-808-809-812",
    "815-819-826-838",
)


# The era name stays registered so datasets written before the interval moved
# to the bundle enumeration still resolve.
@Instance.register("gatsbyjs", "gatsby_815_to_656")
class GATSBY_815_TO_656(Instance):
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

    def parse_log(self, log: str) -> TestResult:
        return parse_ava_tap(log)


for _interval in _BUNDLES:
    Instance.register("gatsbyjs", _interval)(GATSBY_815_TO_656)
