import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image, _safe_path_component
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Apt packages installed into the base image before the repo is cloned.
DEFAULT_PACKAGES = [
    "ca-certificates",
    "curl",
    "build-essential",
    "git",
    "gnupg",
    "make",
    "python3",
    "sudo",
    "wget",
]

# npm install for the test/fix phases. run.sh does NOT install, so any banner
# npm prints appears in only 2 of the 3 phase logs; sending install output to a
# file keeps stdout to test output alone so all three phases agree on test ids.
# A failure aborts the phase rather than being swallowed by `|| true` -- an
# empty result is honest, a fabricated pass is not.
_NPM_QUIET_INSTALL = """npm install --no-audit --no-fund > /tmp/npm-install.log 2>&1 || {
  echo "ERROR: npm install failed"; tail -40 /tmp/npm-install.log; exit 1;
}"""

_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _safe_sha(sha: str) -> str:
    """Validate a commit SHA before it is interpolated into a Dockerfile RUN."""
    if not sha or not _SHA_RE.match(sha):
        raise ValueError(f"unsafe base commit for Dockerfile interpolation: {sha!r}")
    return sha


def clone_and_harden(repo: str, url: str, sha: str) -> str:
    """Clone, pin to the base commit, and destroy all post-base-commit history
    -- in a SINGLE Docker layer.

    Why one layer: Docker layers are append-only. If the clone lands in one RUN
    and the prune in a later RUN, the pre-prune packfile -- which still contains
    every future commit, including the fix -- remains recoverable from the lower
    layer, and the hardening is cosmetic. Doing all of it in one RUN means no
    layer ever holds unpruned history.

    Why in the PR image and not the base: this pins the repo to ONE commit, so
    an image containing it can serve exactly one base SHA. Keeping it here lets
    every PR share a single toolchain-only base image (see ImageBase).

    What it removes: the remote, all refs (heads/remotes/tags/replace), both
    reflogs, and -- via `gc --prune=now` -- the unreachable objects themselves.
    A solver cannot recover the fix through `git log --all`, `git show <sha>`,
    `git cat-file`, `git fsck --lost-found`, the reflog, tags, or packed-refs;
    those objects are gone from the object store, not merely unreferenced.

    The four `test` assertions fail the build if any of that did not hold, so a
    silent hardening regression cannot ship as a usable image.

    Scope: this does NOT stop re-downloading the repo at eval time. Blocking
    `git remote add` + `git fetch` requires network egress control in the runner.
    """
    sha = _safe_sha(sha)
    return f"""RUN set -eux; \\
    git clone "{url}" /home/{repo}; \\
    cd /home/{repo}; \\
    git checkout --detach "{sha}"; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse "{sha}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"; \\
    if [ -f .gitmodules ]; then \\
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
    fi"""


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
        return "node:10"

    # A single shared base image: toolchain only, NO repo checkout. Every PR
    # image inherits it, so the expensive apt layer (incl. the archive.debian.org
    # rewrite) is built once. The repo is cloned and hardened per-PR in
    # ImageDefault, because hardening pins the repo to one commit.
    def image_tag(self) -> str:
        return "base-2x"

    def workdir(self) -> str:
        return "base-2x"

    def files(self) -> list[File]:
        return []

    @staticmethod
    def _is_deprecated_debian(base_img: str) -> bool:
        # node:10 ships Debian buster, whose apt repositories have moved to
        # archive.debian.org, so apt-get update needs the archive rewrite.
        return True

    def dockerfile(self) -> str:
        base_img = self.dependency()
        if isinstance(base_img, Image):
            base_img = base_img.image_full_name()

        packages_str = " \\\n    ".join(DEFAULT_PACKAGES + self.extra_packages())
        # Routes through the archive.debian.org rewrite via _is_deprecated_debian.
        apt_command = self._get_apt_update_command(packages_str, base_img)

        # Validated before interpolation so a repo name carrying shell
        # metacharacters cannot inject commands into the generated build.
        repo = _safe_path_component(self.pr.repo)

        sections = [f"FROM {base_img}"]

        if self.global_env:
            sections.append(self.global_env)

        sections.append(
            "WORKDIR /home/\nENV DEBIAN_FRONTEND=noninteractive\nENV LANG=C.UTF-8"
        )
        sections.append(apt_command)

        if self.clear_env:
            sections.append(self.clear_env)

        sections.append('CMD ["/bin/bash"]')

        return "\n\n".join(sections) + "\n"


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

""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Fail LOUDLY on a broken install -- see the note on _NPM_QUIET_INSTALL. A
# swallowed dependency failure produces a crashed test phase, which the
# classifier then scores as every test being "fixed".
npm install --no-audit --no-fund

# npm install creates/updates package-lock.json. The fix patch ships its own
# lockfile, so the tree must be left clean or `git apply` dies with
# "error: package-lock.json: already exists in working directory" and the fix
# phase captures zero tests. Restore it when tracked; delete it when npm
# generated it (winston 2.x never committed a lockfile).
git checkout -- package-lock.json 2>/dev/null || rm -f package-lock.json

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
npx vows --spec --isolate

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
{npm}
npx vows --spec --isolate

""".format(pr=self.pr, npm=_NPM_QUIET_INSTALL),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{npm}
npx vows --spec --isolate

""".format(pr=self.pr, npm=_NPM_QUIET_INSTALL),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # Validated before interpolation so an org/repo carrying shell
        # metacharacters cannot inject commands into the generated build.
        repo = _safe_path_component(self.pr.repo)
        org = _safe_path_component(self.pr.org, "org")
        url = f"https://github.com/{org}/{repo}.git"

        sections = [f"FROM {name}:{tag}"]

        if self.global_env:
            sections.append(self.global_env)

        sections.append(copy_commands.rstrip("\n"))
        sections.append(clone_and_harden(repo, url, self.pr.base.sha))
        sections.append(f"WORKDIR /home/{repo}")
        sections.append("RUN bash /home/prepare.sh")

        if self.clear_env:
            sections.append(self.clear_env)

        return "\n\n".join(sections) + "\n"


@Instance.register("winstonjs", "winston_1086_to_1086")
class Winston2x(Instance):
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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")

        for line in test_log.splitlines():
            line = ansi_escape.sub("", line).strip()
            if not line or "»" in line:
                continue

            pass_match = re.match(r"^[✓✔]\s+(.+?)(?:\s+\([0-9]+ms\))?$", line)
            if pass_match:
                passed_tests.add(pass_match.group(1).strip())
                continue

            fail_match = re.match(r"^[✗✘]\s+(.+?)(?:\s+\([0-9]+ms\))?$", line)
            if fail_match:
                failed_tests.add(fail_match.group(1).strip())

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# See the matching block in winston.py. Instance.create() routes on
# f"{org}/{number_interval}", so the delivered dash-joined bundle value must be
# registered here or the run fails before any image is built.
#
# This is the winston 2.4 line only (vows + addBatch, node:10). The 3.x bundles
# are registered to Winston in winston.py -- keep the two lists disjoint.
_BUNDLE_NIS = [
    "1086-1188-1253",  # release_line 2.4
]
for _ni in _BUNDLE_NIS:
    Instance.register("winstonjs", _ni)(Winston2x)
