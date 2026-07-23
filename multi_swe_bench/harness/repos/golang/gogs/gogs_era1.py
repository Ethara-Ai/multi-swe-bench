"""Gogs harness for Era 1 — gogits import path, GOPATH mode with symlink.

Covers number_interval: gogs_era1
PRs: 4070, 4168, 4398, 4633, 4707 (base versions v0.9.x–v0.11.43)

Import path: github.com/gogits/gogs
Requires symlink: /gopath/src/github.com/gogits/gogs -> /home/gogs
Test command: GOPATH=/gopath GO111MODULE=off go test -v -count=1 ./...
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Robustly apply the gold test/fix patches for a *bundled* instance.
#
# A bundle's patches are aggregated from many PRs and can carry defects that make
# a plain `git apply` abort before any test runs — which the harness then records
# as a bogus "no test results"/(0,0,0) fix stage (i.e. an unresolvable instance).
# Two such defects were observed in the gogs bundles:
#   1. Binary files serialized as "Binary files a/x and b/x differ" *stubs* with
#      NO embedded data (the patch was generated without `git diff --binary`).
#      These can never apply, but they are vendored assets (e.g. *.gif) that do
#      not affect compiling or running the Go tests, so we strip those blocks.
#   2. The same new file added by BOTH the test patch and the fix patch (vendored
#      files mis-split into both halves), causing "already exists" on the
#      combined apply. We drop those paths from the fix patch.
# We then apply with `--3way` for resilience. If apply STILL fails we exit
# non-zero so the failure surfaces honestly and is never silently masked.
#
# NOTE: only vendored assets / duplicate-adds are removed; every real source and
# test change is applied unchanged, so the pass/fail verdict stays faithful.
_ROBUST_APPLY_SH = r"""#!/bin/bash
set -uo pipefail

repo="$1"; test_patch="$2"; fix_patch="${3:-}"

_strip_binary() {   # <in> <out> : drop any "diff --git" block containing a binary-stub line
    awk '
        function flush(){ if (block != "" && !isbin) printf "%s", block; block=""; isbin=0 }
        /^diff --git /             { flush() }
        /^Binary files .* differ$/ { isbin=1 }
                                   { block = block $0 ORS }
        END                        { flush() }
    ' "$1" > "$2"
}

_drop_paths() {     # <in> <out> <pathlist> : drop blocks whose new path is listed
    awk -v listf="$3" '
        function flush(){ if (block != "" && !(path in drop)) printf "%s", block; block=""; path="" }
        BEGIN { while ((getline l < listf) > 0) drop[l]=1 }
        /^diff --git / { flush(); path=$0; sub(/^diff --git a\//,"",path); sub(/ b\/.*/,"",path) }
                       { block = block $0 ORS }
        END            { flush() }
    ' "$1" > "$2"
}

cd "$repo"

# Docker image layers reset file mtimes/inodes, so git's stat cache is stale and
# perfectly clean files look "modified". Any index-aware apply mode (--index /
# --3way) then aborts with "<file>: does not match index" WITHOUT applying
# anything. Re-sync the worktree and refresh the index before touching patches.
git reset --hard >/dev/null 2>&1 || true
git update-index --refresh >/dev/null 2>&1 || true

_strip_binary "$test_patch" /tmp/_test.patch

if [ -z "$fix_patch" ]; then
    set -- /tmp/_test.patch
else
    grep '^diff --git ' /tmp/_test.patch | sed -E 's#^diff --git a/(.*) b/.*#\1#' | sort -u > /tmp/_testfiles.txt
    _strip_binary "$fix_patch" /tmp/_fix.b.patch
    _drop_paths   /tmp/_fix.b.patch /tmp/_fix.patch /tmp/_testfiles.txt
    set -- /tmp/_test.patch /tmp/_fix.patch
fi

# Plain apply FIRST — this is the original, index-independent behaviour and is
# what already worked for every healthy bundle. Only if it fails do we fall back
# to --3way (now safe, thanks to the index refresh above). This guarantees we can
# never do worse than the pre-existing apply.
git apply --whitespace=nowarn "$@" || git apply --3way --whitespace=nowarn "$@"
"""


class GogsEra1ImageBase(Image):
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
        return "golang:1.9"

    def image_tag(self) -> str:
        return "base-era1"

    def workdir(self) -> str:
        return "base-era1"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        # SHARED base image: built ONCE and reused by every Era-1 gogs PR
        # (image_tag == "base-era1"). It does NOT pass or check out a per-PR
        # BASE_COMMIT — it clones the repo at HEAD via ${REPO_URL} and installs
        # only the COMMON dependencies (system libpam-dev; the Go deps are
        # vendored in the repo and arrive with the clone). Each per-PR image
        # (GogsEra1ImageDefault) builds FROM this base, checks out its own
        # BASE_COMMIT (SHA), and warms the commit-specific build in prepare.sh —
        # reusing everything above from here.
        #
        # The leading `# syntax` directive makes DockerfileEnhancer.enhance()
        # return this Dockerfile unchanged (its first guard is
        # `if SYNTAX_DIRECTIVE in raw: return raw`), so the ARG/ENV/LABEL infra is
        # inlined here and the shared base keeps FULL git history — it is never
        # hardened to a single commit, which would break sibling PRs whose base
        # sha differs. Anti-cheat hardening happens per-PR in GogsEra1ImageDefault.
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}
ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC
ENV LANG=C.UTF-8
LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="ethara.ai"

{self.global_env}

WORKDIR /home/

RUN sed -i 's|deb.debian.org|archive.debian.org|g' /etc/apt/sources.list && sed -i 's|security.debian.org|archive.debian.org|g' /etc/apt/sources.list && sed -i '/stretch-updates/d' /etc/apt/sources.list && apt-get update && apt-get install -y --allow-unauthenticated libpam-dev && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /gopath/src/github.com/gogits

{code}

RUN ln -sf /home/{self.pr.repo} /gopath/src/github.com/gogits/{self.pr.repo}

WORKDIR /home/{self.pr.repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class GogsEra1ImageDefault(Image):
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
        return GogsEra1ImageBase(self.pr, self.config)

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

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

cd /gopath/src/github.com/gogits/{pr.repo}
GOPATH=/gopath GO111MODULE=off go test -v -count=1 ./... || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /gopath/src/github.com/gogits/{pr.repo}
GOPATH=/gopath GO111MODULE=off go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "robust_apply.sh",
                _ROBUST_APPLY_SH,
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

bash /home/robust_apply.sh /home/{pr.repo} /home/test.patch
cd /gopath/src/github.com/gogits/{pr.repo}
GOPATH=/gopath GO111MODULE=off go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

bash /home/robust_apply.sh /home/{pr.repo} /home/test.patch /home/fix.patch
cd /gopath/src/github.com/gogits/{pr.repo}
GOPATH=/gopath GO111MODULE=off go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        # Per-PR anti-cheat hardening. This is the image the model is evaluated
        # in, so after prepare.sh has checked out and warmed BASE_COMMIT we strip
        # every ref/remote and GC unreachable objects: the gold fix/merge commit
        # and the `origin` remote are removed, so a solution cannot recover the
        # fix via `git log`, `git show <future-sha>`, or `git fetch`. BASE_COMMIT
        # is exported as ENV so Image._HARDENING_BLOCK (which references
        # ${BASE_COMMIT}) resolves to THIS PR's base sha.
        return f"""FROM {name}:{tag}

ENV BASE_COMMIT={self.pr.base.sha}

{self.global_env}

{copy_commands}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{prepare_commands}

{Image._HARDENING_BLOCK}

{self.clear_env}

CMD ["/bin/bash"]

"""


@Instance.register("gogs", "gogs_era1")
class GogsEra1(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return GogsEra1ImageDefault(self.pr, self._config)

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

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_pass = re.compile(r"--- PASS: (\S+)")
        re_fail = re.compile(r"--- FAIL: (\S+)")
        re_skip = re.compile(r"--- SKIP: (\S+)")

        for line in clean_log.splitlines():
            line = line.strip()

            m = re_pass.match(line)
            if m:
                passed_tests.add(m.group(1))
                continue

            m = re_fail.match(line)
            if m:
                failed_tests.add(m.group(1))
                continue

            m = re_skip.match(line)
            if m:
                skipped_tests.add(m.group(1))
                continue

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


# ---------------------------------------------------------------------------
# number_interval routing
#
# Instance.create() resolves an instance via f"{org}/{number_interval}" when a
# dataset record carries a `number_interval` field. Each bundle's number_interval
# is the dash-joined, SORTED `prs_in_bundle` (e.g. "146-147-150-155-157") — an
# explicit list of the PR numbers actually in the bundle, NOT a contiguous range
# like "146-157". Register every Era-1 (github.com/gogits) gogs bundle so these records route to
# GogsEra1.
# ---------------------------------------------------------------------------
_NUMBER_INTERVALS = [
    "4168-4170-4181-4185-4194-4294-4312-4330-4343-4344-4345-4353-4361-4372",  # base PR 4168 (14 PRs)
    "4633-4780-4803-4902-4908-4913-4934-4938-4965-4966-4970-4974-4976-4998-5054-5058-5068-5083-5084-5126",  # base PR 4633 (20 PRs)
    "4070-4078-4079-4092-4109",  # base PR 4070 (5 PRs)
    "4398-4405-4412-4417-4423-4436-4440-4451-4460-4474-4490-4492-4519-4540-4548-4549",  # base PR 4398 (16 PRs)
    "4707-5168-5169-5171-5177-5180-5182-5189-5196-5207-5209-5218-5224-5242-5245-5262",  # base PR 4707 (16 PRs)
]
for _ni in _NUMBER_INTERVALS:
    Instance.register("gogs", _ni)(GogsEra1)
